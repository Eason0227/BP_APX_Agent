module.exports = {
  apps: [
    // --- [Agent Data API] ---
    {
      name: "Agent UI OOS-PM-Record-API",
      script: "D:/Paticle_OOB_system/Report_APP_BP/OOS_PM_Record_api.py",
      interpreter: "python",
    }, 
    // --- [Agent API] ---
    {
      name: "QA-Agent-API",
      script: "D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/qa_agent_api.py",
      interpreter: "python",
    },
    {
      name: "PE-Agent-API",
      script: "D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/pe_agent_api.py",
      interpreter: "python",
    },
    {
      name: "EE-Agent-API",
      script: "D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/EE_Agent_api.py",
      interpreter: "python",
    },

    {
      name: "Executive-Agent-API",
      script: "D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/executive_agent_api.py",
      interpreter: "python",
    },

    // --- [Agent app] ---
    {
      name: "Web-Streamlit-EE Agent",
      script: "python",
      args: "-m streamlit run D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/EE_agent_app.py --server.port 8504"
    },
    {
      name: "Web-Streamlit-PE Agent",
      script: "python",
      args: "-m streamlit run D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/pe_agent_app.py --server.port 8505"
    },
    {
      name: "Web-Streamlit-QA Agent",
      script: "python",
      args: "-m streamlit run D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/qa_agent_app.py --server.port 8506"
    },
    {
      name: "Web-Streamlit-APX Agent",
      script: "python",
      args: "-m streamlit run D:/Paticle_OOB_system/Report_APP_BP/APX_Agent/APX_Agent_UI.py --server.port 8503"
    }
  ]
};